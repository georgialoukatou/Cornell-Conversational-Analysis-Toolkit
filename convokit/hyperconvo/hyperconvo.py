from collections import defaultdict
import numpy as np
import scipy.stats

from convokit.transformer import Transformer
from typing import Dict, Optional, Hashable
from convokit.model import Corpus, Utterance
from .hypergraph import Hypergraph
from .triadMotif import TriadMotif, MotifType


class HyperConvo(Transformer):
    """
    Encapsulates computation of hypergraph features for a particular
    corpus.

    fit_transform() retrieves features from the corpus conversational
    threads using retrieve_feats, and stores it in the corpus's conversations'
    meta field under the key "hyperconvo"

    Either use the features directly, or use the other transformers, threadEmbedder (https://zissou.infosci.cornell.edu/socialkit/documentation/threadEmbedder.html)
    or communityEmbedder (https://zissou.infosci.cornell.edu/socialkit/documentation/communityEmbedder.html) to embed threads or communities respectively in a low-dimensional
    space for further analysis or visualization.

    As features, we compute the degree distribution statistics from Table 4 of
    http://www.cs.cornell.edu/~cristian/Patterns_of_participant_interactions.html,
    for both a whole conversation and its midthread, and for indegree and
    outdegree distributions of C->C, C->c and c->c edges, as in the paper.
    We also compute the presence and count of each motif type specified in Fig 2.
    However, we do not include features making use of reaction edges, due to our
    inability to release the Facebook data used in the paper (which reaction
    edges are most naturally suited for). In particular, we do not include edge
    distribution statistics from Table 4, as these rely on the presence of
    reaction edges. We hope to implement a more general version of these
    reaction features in an upcoming release.

    :param prefix_len: Length (in number of utterances) of each thread to
            consider when constructing its hypergraph
    :param min_thread_len: Only consider threads of at least this length
    :param include_root: True if root utterance should be included in the utterance thread,
                         False otherwise, i.e. thread begins from top level comment. (Affects prefix_len and min_thread_len counts.)
                         (If include_root is True, then each Conversation will have metadata for one thread, otherwise each Conversation
                         will have metadata for multiple threads - equal to the number of top-level comments.)
    """

    def __init__(self, prefix_len: int=10, min_thread_len: int=10, include_root: bool=True):
        self.prefix_len = prefix_len
        self.min_thread_len = min_thread_len
        self.include_root = include_root

    def transform(self, corpus: Corpus) -> Corpus:
        """
        Same as fit_transform()
        """
        return self.fit_transform(corpus)

    def fit_transform(self, corpus: Corpus) -> Corpus:
        """
        fit_transform() retrieves features from the corpus conversational
        threads using retrieve_feats()

        :param corpus: Corpus object to retrieve feature information from

        :return: corpus with conversations having a new meta field "hyperconvo" containing the stats generated by retrieve_feats(). Each conversation's metadata then contains the stats for the thread(s) it contains.
        """
        feats = self.retrieve_feats(corpus)
        if self.include_root:  # threads start at root (post)
            for root_id in feats.keys():
                convo = corpus.get_conversation(root_id)
                convo.add_meta("hyperconvo", {root_id: feats[root_id]})
        else:  # threads start at top-level-comment
            # Construct top-level-comment to root mapping
            threads = corpus.utterance_threads(prefix_len=self.prefix_len, include_root=False)

            root_to_tlc = dict()
            for tlc_id, utts in threads.items():
                utt_id = next(iter(utts))
                thread_root = utts[utt_id].root
                if thread_root in root_to_tlc:
                    root_to_tlc[thread_root][tlc_id] = feats[tlc_id]
                else:
                    try:
                        root_to_tlc[thread_root] = {tlc_id: feats[tlc_id]}
                    except KeyError as e:
                        print(str(e))
            for root_id in root_to_tlc:
                convo = corpus.get_conversation(root_id)
                convo.add_meta("hyperconvo", root_to_tlc[root_id])

        return corpus

    @staticmethod
    def _make_hypergraph(corpus: Optional[Corpus] = None,
                         uts: Optional[Dict[Hashable, Utterance]] = None,
                         exclude_id: Hashable = None) -> Hypergraph:
        """
        Construct a Hypergraph from all the utterances of a Corpus, or a specified subset of utterances

        :param corpus: A Corpus to extract utterances from
        :param uts: Subset of utterances to construct a Hypergraph from
        :param exclude_id: id of utterance to exclude from Hypergraph construction

        :return: Hypergraph object
        """
        if uts is None:
            if corpus is None:
                raise RuntimeError("fit_transform() helper method _make_hypergraph()"
                                   "has no valid corpus / utterances input")
            uts = {utt.id: utt for utt in corpus.iter_utterances()}

        G = Hypergraph()
        username_to_utt_ids = dict()
        reply_edges = []
        speaker_to_reply_tos = defaultdict(list)
        speaker_target_pairs = set()
        # nodes
        for ut in sorted(uts.values(), key=lambda h: h.get("timestamp")):
            if ut.id != exclude_id:
                if ut.user not in username_to_utt_ids:
                    username_to_utt_ids[ut.user] = set()
                username_to_utt_ids[ut.user].add(ut.id)
                if ut.reply_to is not None and ut.reply_to in uts \
                        and ut.reply_to != exclude_id:
                    reply_edges.append((ut.id, ut.reply_to))
                    speaker_to_reply_tos[ut.user].append(ut.reply_to)
                    speaker_target_pairs.add((ut.user, uts[ut.reply_to].user, ut.timestamp, ut.text, ut.reply_to, ut.id, ut.root))
                G.add_node(ut.id, info=ut.__dict__)
        # hypernodes
        for u, ids in username_to_utt_ids.items():
            G.add_hypernode(u, ids, info=u.meta)
        # reply edges
        for u, v in reply_edges:
            # print("ADDING TIMESTAMP")
            G.add_edge(u, v)
        # user to utterance response edges
        for u, reply_tos in speaker_to_reply_tos.items():
            for reply_to in reply_tos:
                G.add_edge(u, reply_to)
        # user to user response edges
        for u, v, timestamp, text, reply_to, utt_id, top_level_comment in speaker_target_pairs:
            G.add_edge(u, v, {'timestamp': timestamp,
                              'text': text,
                              'speaker': u,
                              'target': v,
                              'reply_to': reply_to,
                              'top_level_comment': top_level_comment,
                              'utt_id': utt_id,
                              'root': (reply_to == top_level_comment)
                              })
        return G

    @staticmethod
    def _node_type_name(b: bool) -> str:
        """
        Helper method to get node type name (C or c)

        :param b: Bool, where True indicates node is a Hypernode
        :return: "C" if True, "c" if False
        """
        return "C" if b else "c"

    @staticmethod
    def _degree_feats(uts: Optional[Dict[Hashable, Utterance]]=None,
                      G: Optional[Hypergraph]=None,
                      name_ext: str="",
                      exclude_id: Optional[Hashable]=None) -> Dict:
        """
        Helper method for retrieve_feats().
        Generate statistics on degree-related features in a Hypergraph (G), or a Hypergraph
        constructed from provided utterances (uts)

        :param uts: utterances to construct Hypergraph from
        :param G: Hypergraph to calculate degree features statistics from
        :param name_ext: Suffix to append to feature name
        :param exclude_id: id of utterance to exclude from Hypergraph construction
        :return: A stats dictionary, i.e. a dictionary of feature names to feature values. For degree-related features specifically.
        """
        assert uts is None or G is None
        if G is None:
            G = HyperConvo._make_hypergraph(uts, exclude_id=exclude_id)

        stat_funcs = {
            "max": np.max,
            "argmax": np.argmax,
            "norm.max": lambda l: np.max(l) / np.sum(l),
            "2nd-largest": lambda l: np.partition(l, -2)[-2] if len(l) > 1
            else np.nan,
            "2nd-argmax": lambda l: (-l).argsort()[1] if len(l) > 1 else np.nan,
            "norm.2nd-largest": lambda l: np.partition(l, -2)[-2] / np.sum(l)
            if len(l) > 1 else np.nan,
            "mean": np.mean,
            "mean-nonzero": lambda l: np.mean(l[l != 0]),
            "prop-nonzero": lambda l: np.mean(l != 0),
            "prop-multiple": lambda l: np.mean(l[l != 0] > 1),
            "entropy": scipy.stats.entropy,
            "2nd-largest / max": lambda l: np.partition(l, -2)[-2] / np.max(l)
            if len(l) > 1 else np.nan
        }

        stats = {}
        for from_hyper in [False, True]:
            for to_hyper in [False, True]:
                if not from_hyper and to_hyper: continue  # skip c -> C
                outdegrees = np.array(G.outdegrees(from_hyper, to_hyper))
                indegrees = np.array(G.indegrees(from_hyper, to_hyper))

                for stat, stat_func in stat_funcs.items():
                    stats["{}[outdegree over {}->{} {}responses]".format(stat,
                                                                         HyperConvo._node_type_name(from_hyper),
                                                                         HyperConvo._node_type_name(to_hyper),
                                                                         name_ext)] = stat_func(outdegrees)
                    stats["{}[indegree over {}->{} {}responses]".format(stat,
                                                                        HyperConvo._node_type_name(from_hyper),
                                                                        HyperConvo._node_type_name(to_hyper),
                                                                        name_ext)] = stat_func(indegrees)
        return stats

    @staticmethod
    def probabilities(transitions: Dict):
        """
        Takes a transitions count dictionary Dict[(MotifType.name->MotifType.name)->Int]
        :return: transitions probability dictionary Dict[(MotifType.name->MotifType.name)->Float]
        """
        probs = dict()

        for parent, children in TriadMotif.relations().items():
            total = sum(transitions[(parent, c)] for c in children) + transitions[(parent, parent)]
            probs[(parent, parent)] = (transitions[(parent, parent)] / total) if total > 0 else 0
            for c in children:
                probs[(parent, c)] = (transitions[(parent, c)] / total) if total > 0 else 0

        return probs

    @staticmethod
    def _latent_motif_count(motifs, trans: bool):
        """
        Takes a dictionary of (MotifType.name, List[Motif]) and a bool prob, indicating whether
        transition probabilities need to be returned
        :return: Returns a tuple of a dictionary of latent motif counts
        and a dictionary of motif->motif transition probabilities
         (Dict[MotifType.name->Int], Dict[(MotifType.name->MotifType.name)->Float])
         The second element is None if prob=False
        """
        latent_motif_count = {motif_type.name: 0 for motif_type in MotifType}

        transitions = TriadMotif.transitions()
        for motif_type, motif_instances in motifs.items():
            for motif_instance in motif_instances:
                curr_motif = motif_instance
                child_motif_type = curr_motif.get_type()
                # Reflexive edge
                if trans:
                    transitions[(child_motif_type, child_motif_type)] += 1

                # print(transitions)
                while True:
                    latent_motif_count[curr_motif.get_type()] +=  1
                    curr_motif = curr_motif.regress()
                    if curr_motif is None: break
                    parent_motif_type = curr_motif.get_type()
                    if trans:
                        transitions[(parent_motif_type, child_motif_type)] += 1
                    child_motif_type = parent_motif_type

        return latent_motif_count, transitions

    @staticmethod
    def _motif_feats(uts: Optional[Dict[Hashable, Utterance]] = None,
                     G: Hypergraph = None,
                     name_ext: str="",
                     exclude_id: str = None,
                     latent=False,
                     trans=False) -> Dict:
        """
        Helper method for retrieve_feats().
        Generate statistics on motif-related features in a Hypergraph (G), or a Hypergraph
        constructed from provided utterances (uts)
        :param uts: utterances to construct Hypergraph from
        :param G: Hypergraph to calculate degree features statistics from
        :param name_ext: Suffix to append to feature name
        :param exclude_id: id of utterance to exclude from Hypergraph construction
        :return: A dictionary from a thread root id to its stats dictionary, which is a dictionary from feature names to feature values. For motif-related features specifically.
        """
        assert uts is None or G is None
        if G is None:
            G = HyperConvo._make_hypergraph(uts=uts, exclude_id=exclude_id)

        motifs = G.extract_motifs()

        stat_funcs = {
            "count": len
        }

        stats = {}

        for motif_type in motifs:
            for stat, stat_func in stat_funcs.items():
                stats["{}[{}{}]".format(stat, str(motif_type), name_ext)] = \
                    stat_func(motifs[motif_type])

        if latent:
            latent_motif_count, transitions = HyperConvo._latent_motif_count(motifs, trans=trans)
            for motif_type in latent_motif_count:
                # stats["is-present[LATENT_{}{}]".format(motif_type, name_ext)] = \
                #     (latent_motif_count[motif_type] > 0)
                stats["count[LATENT_{}{}]".format(motif_type, name_ext)] = latent_motif_count[motif_type]

            if trans:
                assert transitions is not None
                for p, v in transitions.items():
                    stats["trans[{}]".format(p)] = v

        return stats


    def retrieve_motifs(self, corpus: Corpus):
        threads_motifs = {}
        for i, (root, thread) in enumerate(
                corpus.utterance_threads(prefix_len=self.prefix_len, include_root=self.include_root).items()):
            if len(thread) < self.min_thread_len: continue
            G = HyperConvo._make_hypergraph(uts=thread)
            motifs = G.extract_motifs()
            threads_motifs[root] = motifs

        return threads_motifs

    def retrieve_motif_paths(self, corpus: Corpus):
        threads_motifs = self.retrieve_motifs(corpus)
        threads_paths = dict()

        for thread_id, motif_dict in threads_motifs.items():
            thread_paths_dict = defaultdict(list)
            for motif_type, instances in motif_dict.items():
                for instance in instances:
                    thread_paths_dict[instance.get_development_path()].append(instance)
            threads_paths[thread_id] = thread_paths_dict

        return threads_paths

    def retrieve_motif_pathway_stats(self, corpus: Corpus):
        threads_motifs = self.retrieve_motifs(corpus)
        paths_list = TriadMotif.paths_list()
        paths_dict = {k: 0 for k in paths_list}

        threads_stats = dict()

        for thread_id, motif_dict in threads_motifs.items():
            thread_paths_dict = paths_dict.copy()
            for motif_type, instances in motif_dict.items():
                for instance in instances:
                    thread_paths_dict[instance.get_development_path()] += 1
            threads_stats[thread_id] = thread_paths_dict

        return threads_stats

    def retrieve_motif_counts(self, corpus: Corpus):
        threads_motifs = self.retrieve_motifs(corpus)
        for thread_id, motif_dict in threads_motifs.items():
            for motif_type, instances in motif_dict.items():
                motif_dict[motif_type] = len(instances)
        return threads_motifs


    def retrieve_motif_texts(self, corpus: Corpus):
        threads_motifs = self.retrieve_motifs(corpus)

        for thread_id, motif_dict in threads_motifs.items():
            for motif_type, instances in motif_dict.items():
                motif_dict[motif_type] = [motif.get_text() for motif in instances]
        return threads_motifs


    def retrieve_feats(self, corpus: Corpus) -> Dict[Hashable, Dict]:
        """
        Retrieve all hypergraph features for a given corpus (viewed as a set
        of conversation threads).

        See init() for further documentation.

        :return: A dictionary from a thread root id to its stats dictionary,
            which is a dictionary from feature names to feature values. For degree-related
            features specifically.
        """

        threads_stats = dict()

        for i, (root, thread) in enumerate(
                corpus.utterance_threads(prefix_len=self.prefix_len, include_root=self.include_root).items()):
            if len(thread) < self.min_thread_len: continue
            stats = {}
            G = HyperConvo._make_hypergraph(uts=thread)
            G_mid = HyperConvo._make_hypergraph(uts=thread, exclude_id=root)
            for k, v in HyperConvo._degree_feats(G=G).items(): stats[k] = v
            # for k, v in HyperConvo._motif_feats(G=G).items(): stats[k] = v
            for k, v in HyperConvo._degree_feats(G=G_mid,
                                           name_ext="mid-thread ").items(): stats[k] = v
            # for k, v in HyperConvo._motif_feats(G=G_mid,
            #                               name_ext=" over mid-thread").items(): stats[k] = v
            threads_stats[root] = stats

        return threads_stats

